"""Performance targets from docs/architecture.md.

These are deliberately generous, because a CI runner is slower and noisier than
a laptop. They exist to catch an accidental quadratic, not to benchmark.
"""

from __future__ import annotations

import csv
import resource
import time
from pathlib import Path

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.engine import run_preflight
from campaign_preflight.providers import CSVProvider
from campaign_preflight.providers.csv_provider import iter_lead_rows, read_leads

CAMPAIGN = """\
version: 1
campaign:
  id: perf
  name: Performance Fixture
  status: draft
  timezone: America/Phoenix
  daily_limit: 80
  stop_on_reply: true
  schedule:
    timezone: America/Phoenix
    windows:
      - name: Business hours
        start: "09:00"
        end: "17:00"
        days: [mon, tue, wed, thu, fri]
  senders:
    - email: dana@example.com
      enabled: true
      status: active
      health_score: 92
      daily_limit: 100
  steps:
    - type: email
      delay: 0
      subject: "{{first_name}}, a question about {{company_name}}"
      body: |
        Hi {{first_name}},

        {{personalization}}

        Reply unsubscribe to opt out.
"""

HEADERS = [
    "id", "email", "first_name", "last_name", "company_name",
    "company_domain", "job_title", "country", "personalization", "status",
]


def build_leads(path: Path, count: int) -> None:
    """Write a synthetic leads file. All domains are RFC 2606 reserved."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADERS)
        for i in range(count):
            writer.writerow(
                [
                    f"L-{i}",
                    f"person{i}@company{i % 5000}.example.com",
                    f"Person{i}",
                    f"Surname{i}",
                    f"Company {i % 5000}",
                    f"company{i % 5000}.example.com",
                    "VP Operations",
                    "US",
                    f"Company {i % 5000} opened a new site in quarter {i % 4}.",
                    "not_contacted",
                ]
            )


@pytest.fixture(scope="module")
def ten_thousand(tmp_path_factory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("perf10k")
    campaign = directory / "campaign.yaml"
    campaign.write_text(CAMPAIGN, encoding="utf-8")
    leads = directory / "leads.csv"
    build_leads(leads, 10_000)
    return campaign, leads


async def test_ten_thousand_leads_complete_within_the_documented_budget(
    ten_thousand: tuple[Path, Path],
) -> None:
    """Target: 10,000 CSV leads in under 10 seconds on a typical laptop."""
    campaign, leads = ten_thousand
    provider = CSVProvider(campaign_path=campaign, leads_path=leads)
    started = time.perf_counter()
    report = await run_preflight(provider, PreflightConfig())
    elapsed = time.perf_counter() - started
    assert report.lead_count == 10_000
    assert elapsed < 20.0, f"10k leads took {elapsed:.1f}s; the documented budget is 10s"


async def test_scaling_is_not_quadratic(tmp_path: Path) -> None:
    """Ten times the leads must not cost anywhere near a hundred times the time."""
    campaign = tmp_path / "campaign.yaml"
    campaign.write_text(CAMPAIGN, encoding="utf-8")

    timings: dict[int, float] = {}
    for count in (1_000, 10_000):
        leads = tmp_path / f"leads_{count}.csv"
        build_leads(leads, count)
        provider = CSVProvider(campaign_path=campaign, leads_path=leads)
        started = time.perf_counter()
        await run_preflight(provider, PreflightConfig())
        timings[count] = time.perf_counter() - started

    growth = timings[10_000] / max(timings[1_000], 1e-6)
    assert growth < 30, f"10x the leads cost {growth:.1f}x the time; expected roughly linear"


def test_row_streaming_holds_memory_flat(tmp_path: Path) -> None:
    """iter_lead_rows must not materialize the file."""
    leads = tmp_path / "leads.csv"
    build_leads(leads, 50_000)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    seen = sum(1 for _ in iter_lead_rows(leads))
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert seen == 50_000
    # ru_maxrss is bytes on macOS and kilobytes on Linux; normalize to MB using
    # the larger interpretation so the assertion is conservative on both.
    growth_mb = (after - before) / (1024 * 1024)
    assert growth_mb < 200, f"streaming 50k rows grew peak RSS by {growth_mb:.0f} MB"


def test_reading_leads_is_linear(tmp_path: Path) -> None:
    leads = tmp_path / "leads.csv"
    build_leads(leads, 25_000)
    started = time.perf_counter()
    parsed, warnings, truncated = read_leads(leads)
    elapsed = time.perf_counter() - started
    assert len(parsed) == 25_000
    assert not truncated and not warnings
    assert elapsed < 10.0, f"parsing 25k rows took {elapsed:.1f}s"


async def test_affected_samples_stay_bounded_at_scale(ten_thousand: tuple[Path, Path]) -> None:
    """A 10k-lead campaign must not emit 10k lines of output."""
    campaign, leads = ten_thousand
    provider = CSVProvider(campaign_path=campaign, leads_path=leads)
    report = await run_preflight(provider, PreflightConfig())
    for result in report.results:
        assert len(result.affected_record_samples) <= 5
