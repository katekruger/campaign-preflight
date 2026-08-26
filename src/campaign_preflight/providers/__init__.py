"""Read-only providers. Nothing in this package performs a write.

The Instantly provider is imported lazily: it needs the optional ``httpx``
dependency, and the demo, file checks, and MCP server must work without it.
"""

from .base import (
    CampaignProvider,
    ProviderResult,
    failed,
    forbidden,
    misconfigured,
    ok,
    unsupported,
)
from .csv_provider import CSVProvider
from .fixture_provider import FixtureProvider, demo_paths

__all__ = [
    "CSVProvider",
    "CampaignProvider",
    "FixtureProvider",
    "ProviderResult",
    "demo_paths",
    "failed",
    "forbidden",
    "instantly_available",
    "misconfigured",
    "ok",
    "unsupported",
]


def instantly_available() -> bool:
    """True when the optional Instantly dependency is installed."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True
