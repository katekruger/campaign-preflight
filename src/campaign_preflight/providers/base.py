"""The read-only provider contract.

Every provider returns a :class:`ProviderResult`, never a bare value. That is the
whole point of this module: a provider must be able to say "I looked and there
was nothing" separately from "I could not look". Rules key off that distinction
to choose between ``PASS`` and ``UNKNOWN``.

No method on this protocol writes. There is deliberately no ``create_*``,
``update_*``, ``activate_*``, or ``delete_*`` anywhere in the package.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ..models import (
    Campaign,
    Capability,
    CapabilityReport,
    CapabilityStatus,
    Lead,
    PersonalizationClaim,
    ProviderMetadata,
    Sender,
    SourceEvidence,
    SuppressionEntry,
    utcnow,
)

__all__ = [
    "CampaignProvider",
    "ProviderResult",
    "failed",
    "forbidden",
    "misconfigured",
    "ok",
    "unsupported",
]

T = TypeVar("T")


# slots=True would require Python 3.10; the Cowork plugin targets 3.9.
@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """A value plus *why* it does or does not exist.

    ``data`` is only meaningful when ``status`` is ``SUPPORTED_OK``. For any
    other status the payload is ``None`` and callers must treat the underlying
    question as unanswered.
    """

    capability: Capability
    status: CapabilityStatus
    data: T | None = None
    detail: str | None = None
    partial: bool = False
    """True when a successful read was truncated (page cap, row cap, timeout)."""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status.is_ok

    def unwrap_or(self, default: T) -> T:
        """The data when the read succeeded, else ``default``.

        Only safe where an empty default is genuinely equivalent for the caller;
        rules must consult :attr:`status` instead.
        """
        return self.data if (self.is_ok and self.data is not None) else default

    def to_report(self, record_count: int | None = None) -> CapabilityReport:
        return CapabilityReport(
            capability=self.capability,
            status=self.status,
            detail=self.detail,
            record_count=record_count,
        )


def ok(
    capability: Capability,
    data: T,
    *,
    detail: str | None = None,
    partial: bool = False,
    **metadata: Any,
) -> ProviderResult[T]:
    return ProviderResult(
        capability=capability,
        status=CapabilityStatus.SUPPORTED_OK,
        data=data,
        detail=detail,
        partial=partial,
        metadata=metadata,
    )


def failed(capability: Capability, detail: str) -> ProviderResult[Any]:
    """The capability exists but this attempt to read it did not work."""
    return ProviderResult(
        capability=capability, status=CapabilityStatus.SUPPORTED_FAILED, detail=detail
    )


def unsupported(capability: Capability, detail: str) -> ProviderResult[Any]:
    """This provider cannot supply the capability at all."""
    return ProviderResult(capability=capability, status=CapabilityStatus.UNSUPPORTED, detail=detail)


def forbidden(capability: Capability, detail: str) -> ProviderResult[Any]:
    """Credentials are valid but lack the scope or plan for this capability."""
    return ProviderResult(
        capability=capability, status=CapabilityStatus.UNAVAILABLE_PERMISSIONS, detail=detail
    )


def misconfigured(capability: Capability, detail: str) -> ProviderResult[Any]:
    """The run was not given what it needed (a file path, a key, an id)."""
    return ProviderResult(
        capability=capability, status=CapabilityStatus.UNAVAILABLE_CONFIG, detail=detail
    )


class CampaignProvider(abc.ABC):
    """Read-only source of campaign inspection data.

    Implementations must never perform a mutating operation. Subclasses that talk
    to a network API are additionally expected to enforce a transport-level
    allowlist so a coding mistake cannot become a write.
    """

    name: str = "unknown"
    version: str | None = None
    base_url: str | None = None

    # -- required ----------------------------------------------------------

    @abc.abstractmethod
    async def get_campaign(self, campaign_id: str | None = None) -> ProviderResult[Campaign]:
        """The campaign under inspection."""

    @abc.abstractmethod
    async def list_campaign_leads(
        self, campaign_id: str | None = None, *, limit: int | None = None
    ) -> ProviderResult[list[Lead]]:
        """Leads attached to the campaign, normalized."""

    @abc.abstractmethod
    async def list_campaign_senders(
        self, campaign_id: str | None = None
    ) -> ProviderResult[list[Sender]]:
        """Sending mailboxes attached to the campaign."""

    # -- optional ----------------------------------------------------------

    async def get_sender_health(self, senders: list[Sender]) -> ProviderResult[list[Sender]]:
        """Senders enriched with health/warmup data.

        Default: unsupported. Providers that cannot measure deliverability must
        leave this alone rather than inventing a score.
        """
        return unsupported(
            Capability.SENDER_HEALTH,
            f"{self.name} does not expose sender health data",
        )

    async def list_suppressions(self) -> ProviderResult[list[SuppressionEntry]]:
        """Suppression / block-list entries."""
        return unsupported(
            Capability.SUPPRESSIONS, f"{self.name} does not expose a suppression list"
        )

    async def get_campaign_analytics_context(
        self, campaign_id: str | None = None
    ) -> ProviderResult[dict[str, Any]]:
        """Aggregate counts used to cross-check lead totals."""
        return unsupported(Capability.ANALYTICS, f"{self.name} does not expose campaign analytics")

    async def list_evidence(self) -> ProviderResult[list[SourceEvidence]]:
        """Research evidence backing personalization claims."""
        return unsupported(Capability.EVIDENCE, f"{self.name} does not carry evidence documents")

    async def list_claims(self) -> ProviderResult[list[PersonalizationClaim]]:
        """Explicit personalization claims, when the input supplies them."""
        return unsupported(
            Capability.EVIDENCE, f"{self.name} does not carry personalization claims"
        )

    async def health_check(self) -> ProviderResult[dict[str, Any]]:
        """Cheap reachability probe. Never required for a run to proceed."""
        return ok(Capability.CAMPAIGN, {"provider": self.name, "reachable": True})

    async def aclose(self) -> None:
        """Release any held resources. Safe to call more than once."""
        return None

    # -- helpers -----------------------------------------------------------

    def metadata(
        self,
        capabilities: list[CapabilityReport],
        errors: list[str] | None = None,
    ) -> ProviderMetadata:
        return ProviderMetadata(
            name=self.name,
            version=self.version,
            base_url=self.base_url,
            read_only=True,
            capabilities=tuple(capabilities),
            errors=tuple(errors or ()),
            fetched_at=utcnow(),
        )

    async def __aenter__(self) -> CampaignProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
