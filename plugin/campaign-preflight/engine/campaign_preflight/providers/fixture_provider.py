"""Deterministic provider for the bundled demo and for tests.

Two ways to use it:

* :meth:`FixtureProvider.demo` reads the synthetic files shipped inside the
  package. It performs no network I/O of any kind -- that property is asserted
  by ``tests/unit/test_demo_offline.py``.
* The constructor accepts explicit :class:`ProviderResult` objects, which lets a
  test reproduce any capability state (permission denied, endpoint failed,
  unsupported) without a live provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import (
    Campaign,
    Capability,
    Lead,
    PersonalizationClaim,
    Sender,
    SourceEvidence,
    SuppressionEntry,
)
from .base import CampaignProvider, ProviderResult, ok, unsupported
from .csv_provider import CSVProvider

__all__ = ["FixtureProvider", "demo_paths", "DEMO_DIR"]

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def demo_paths() -> dict[str, Path]:
    """Absolute paths to the bundled demo files."""
    return {
        "campaign": DEMO_DIR / "campaign.yaml",
        "leads": DEMO_DIR / "leads.csv",
        "suppressions": DEMO_DIR / "suppressions.csv",
        "evidence": DEMO_DIR / "evidence.json",
    }


class FixtureProvider(CampaignProvider):
    """In-memory provider with fully controllable capability outcomes."""

    name = "fixture"

    def __init__(
        self,
        *,
        campaign: ProviderResult[Campaign] | None = None,
        leads: ProviderResult[list[Lead]] | None = None,
        senders: ProviderResult[list[Sender]] | None = None,
        sender_health: ProviderResult[list[Sender]] | None = None,
        suppressions: ProviderResult[list[SuppressionEntry]] | None = None,
        evidence: ProviderResult[list[SourceEvidence]] | None = None,
        claims: ProviderResult[list[PersonalizationClaim]] | None = None,
        analytics: ProviderResult[dict[str, Any]] | None = None,
        name: str | None = None,
    ) -> None:
        if name:
            self.name = name
        self._campaign = campaign or unsupported(
            Capability.CAMPAIGN, "fixture supplied no campaign"
        )
        self._leads = leads or unsupported(Capability.LEADS, "fixture supplied no leads")
        self._senders = senders or unsupported(
            Capability.SENDERS, "fixture supplied no senders"
        )
        self._sender_health = sender_health
        self._suppressions = suppressions or unsupported(
            Capability.SUPPRESSIONS, "fixture supplied no suppression list"
        )
        self._evidence = evidence or unsupported(
            Capability.EVIDENCE, "fixture supplied no evidence"
        )
        self._claims = claims or unsupported(
            Capability.EVIDENCE, "fixture supplied no claims"
        )
        self._analytics = analytics or unsupported(
            Capability.ANALYTICS, "fixture supplied no analytics"
        )

    # -- factory ------------------------------------------------------------

    @classmethod
    def demo(cls) -> CSVProvider:
        """The bundled synthetic campaign, read through the real CSV path.

        Returns a :class:`CSVProvider` rather than a hand-built fixture so the
        demo exercises the same parsing, validation, and normalization code a
        real user's files go through. All demo data is synthetic; see
        ``docs/demo.md``.
        """
        paths = demo_paths()
        provider = CSVProvider(
            campaign_path=paths["campaign"],
            leads_path=paths["leads"],
            suppressions_path=paths["suppressions"],
            evidence_path=paths["evidence"],
        )
        provider.name = "demo"
        return provider

    # -- provider interface -------------------------------------------------

    async def get_campaign(self, campaign_id: str | None = None) -> ProviderResult[Campaign]:
        return self._campaign

    async def list_campaign_leads(
        self, campaign_id: str | None = None, *, limit: int | None = None
    ) -> ProviderResult[list[Lead]]:
        result = self._leads
        if limit is not None and result.is_ok and result.data and len(result.data) > limit:
            return ok(
                Capability.LEADS,
                result.data[:limit],
                detail=result.detail,
                partial=True,
            )
        return result

    async def list_campaign_senders(
        self, campaign_id: str | None = None
    ) -> ProviderResult[list[Sender]]:
        return self._senders

    async def get_sender_health(self, senders: list[Sender]) -> ProviderResult[list[Sender]]:
        if self._sender_health is not None:
            return self._sender_health
        return await super().get_sender_health(senders)

    async def list_suppressions(self) -> ProviderResult[list[SuppressionEntry]]:
        return self._suppressions

    async def list_evidence(self) -> ProviderResult[list[SourceEvidence]]:
        return self._evidence

    async def list_claims(self) -> ProviderResult[list[PersonalizationClaim]]:
        return self._claims

    async def get_campaign_analytics_context(
        self, campaign_id: str | None = None
    ) -> ProviderResult[dict[str, Any]]:
        return self._analytics
